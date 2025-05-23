import os
import io
import requests
import psycopg2
import pandas as pd
import markdown as mdlib
from bs4 import BeautifulSoup
import re
from datetime import datetime
from urllib.parse import quote

from flask import (
    Flask, render_template, request,
    jsonify, make_response
)
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer
)
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from dotenv import load_dotenv


# =========== Настройки папки для загрузки CSV ==========
UPLOAD_FOLDER = '/opt/airflow/dags/src/flask_app/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# =========== Регистрируем шрифты ==========
# Положите файлы Lato-Regular.ttf и DejaVuSans.ttf рядом с app.py
script_dir = os.path.dirname(os.path.abspath(__file__))
# Lato для латиницы
lato_path = os.path.join(script_dir, 'Lato-Regular.ttf')
if not os.path.isfile(lato_path):
    raise FileNotFoundError(f"Font file not found: {lato_path}. Place Lato-Regular.ttf next to app.py.")
pdfmetrics.registerFont(TTFont('Lato', lato_path))
# DejaVuSans для кириллицы
dejavu_path = os.path.join(script_dir, 'DejaVuSans.ttf')
if not os.path.isfile(dejavu_path):
    raise FileNotFoundError(f"Font file not found: {dejavu_path}. Place DejaVuSans.ttf next to app.py.")
pdfmetrics.registerFont(TTFont('DejaVuSans', dejavu_path))

# =========== Параметры БД (замените на свои) ==========
load_dotenv()

DB_HOST = os.getenv('POSTGRES_WH_HOST')
DB_PORT = os.getenv('POSTGRES_WH_PORT')
DB_NAME = os.getenv('POSTGRES_WH_DB')
DB_USER = os.getenv('POSTGRES_WH_USER')
DB_PASS = os.getenv('POSTGRES_WH_PASSWORD')


def get_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        dbname=DB_NAME, user=DB_USER,
        password=DB_PASS
    )


app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

TABLE_SCHEMAS = {
    'products': ['row_id','product_id','product_name','category','subcategory'],
    'shipment': ['ship_id','ship_date','ship_mode','ship_cost','quantity','deal_id'],
    'orders':   ['row_id','deal_id','customer_id','order_priority'],
    'sales':    ['deal_id','deal_date','product_id','market','sale_price','discount','profit'],
    'location_info': ['loc_info_id','city','region','country','postal_code'],
    'customers': ['row_id','customer_id','customer_name','segment','loc_info_id']
}


@app.route('/')
def home():
    return render_template('home.html')


@app.route('/import', methods=['GET', 'POST'])
def import_csv():
    tables = list(TABLE_SCHEMAS.keys())
    if request.method == 'POST':
        file = request.files.get('file')
        table = request.form.get('table','')
        if not file or not file.filename.lower().endswith('.csv'):
            return jsonify({'status': 'error', 'message': 'Только CSV'}), 400

        path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
        file.save(path)

        conf = {"filename": file.filename}
        if table:
            conf["table"] = table

        resp = requests.post(
            "http://airflow-webserver:8080/api/v1/dags/etl_csv_to_file/dagRuns",
            auth=("xam","crashes2025"),
            json={"conf": conf}
        )
        if resp.ok:
            msg = f"«{file.filename}» загружен"
            msg += f", таблица «{table}»" if table else ", общий импорт"
            msg += f", DAG стартовал (код {resp.status_code})"
            return jsonify({'status':'success','message':msg})
        else:
            return jsonify({'status':'error',
                'message':f"DAG не запустился ({resp.status_code})"}),500

    return render_template('import.html', tables=tables)


@app.route('/manual')
def manual():
    return render_template('manual.html')


@app.route('/api/<table_name>', methods=['POST'])
def api_insert(table_name):
    if table_name not in TABLE_SCHEMAS:
        return jsonify({'status':'error','message':'Неверная таблица'}),400
    data = request.get_json()
    columns = TABLE_SCHEMAS[table_name]
    values = []
    for col in columns:
        if col not in data:
            return jsonify({'status':'error','message':f'Отсутствует поле {col}'}),400
        values.append(data[col])

    cols_sql = ','.join(columns)
    placeholders = ','.join(['%s']*len(columns))
    sql = f"INSERT INTO {table_name} ({cols_sql}) VALUES ({placeholders})"
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, values)
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'status':'success','message':'Данные сохранены'})
    except Exception as e:
        return jsonify({'status':'error','message':str(e)}),500


@app.route('/tables/')
@app.route('/tables/<table_name>')
def show_table(table_name=None):
    tables = list(TABLE_SCHEMAS.keys())
    data = columns = error = None

    if table_name:
        if table_name not in tables:
            error = f"Таблица «{table_name}» не найдена."
        else:
            try:
                df = pd.read_sql(f"SELECT * FROM {table_name}", get_connection())
                data = df.to_dict(orient='records')
                columns = df.columns.tolist()
            except Exception as e:
                error = f"Ошибка загрузки «{table_name}»: {e}"

    return render_template(
        'tables.html',
        tables=tables,
        active=table_name,
        data=data,
        columns=columns,
        error=error
    )


@app.route('/report', methods=['GET','POST'])
def report():
    if request.method == 'POST':
        text_md = request.form.get('markdown','')
        html = mdlib.markdown(text_md)
        html = html.replace('<strong>','<b>').replace('</strong>','</b>')
        html = html.replace('<em>','<i>').replace('</em>','</i>')
        soup = BeautifulSoup(html, 'html.parser')
        blocks = soup.find_all(['h1','h2','h3','p','li'])

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=20*mm, leftMargin=20*mm,
            topMargin=20*mm, bottomMargin=20*mm
        )
        styles = getSampleStyleSheet()
        flow = []
        for elem in blocks:
            if elem.name == 'h1': style = styles['Heading1']
            elif elem.name == 'h2': style = styles['Heading2']
            elif elem.name == 'h3': style = styles['Heading3']
            else: style = styles['Normal']

            text = elem.get_text()
            if elem.name == 'li': text = '• ' + text
            font = 'DejaVuSans' if re.search(r'[\u0400-\u04FF]', text) else 'Lato'
            style.fontName = font
            flow.append(Paragraph(text, style))
            flow.append(Spacer(1, 4*mm))

        doc.build(flow)
        pdf = buffer.getvalue()
        buffer.close()
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"Отчет_{timestamp}.pdf"
        ascii_filename = f"report_{timestamp}.pdf"
        encoded = quote(filename)
        resp = make_response(pdf)
        resp.headers['Content-Type'] = 'application/pdf'
        resp.headers['Content-Disposition'] = f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{encoded}"
        return resp
    return render_template('report.html')


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)
