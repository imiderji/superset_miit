from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime
import pandas as pd
import numpy as np
import os
import psycopg2
from psycopg2.extras import execute_values

# Папка, куда Flask сохраняет CSV
UPLOAD_FOLDER = '/opt/airflow/dags/src/flask_app/uploads'

# Параметры подключения к базе
DB_HOST = "89.169.3.174"
DB_PORT = "5434"
DB_NAME = "warehouse_superset"
DB_USER = "wh_admin_xam"
DB_PASS = "красавчики2025"

def get_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        dbname=DB_NAME, user=DB_USER,
        password=DB_PASS
    )

def extract(**kwargs):
    conf = kwargs['dag_run'].conf or {}
    filename = conf.get('filename')
    table = conf.get('table')
    if not filename:
        raise ValueError("Filename not specified in DAG run conf")
    if not table:
        raise ValueError("Target table not specified in DAG run conf")

    df = pd.read_csv(
        os.path.join(UPLOAD_FOLDER, filename),
        na_values=['NULL', ''],
        keep_default_na=True
    )

    ti = kwargs['ti']
    ti.xcom_push(key='raw_data', value=df.to_json())
    ti.xcom_push(key='table', value=table)

def transform(**kwargs):
    ti = kwargs['ti']
    df = pd.read_json(ti.xcom_pull(key='raw_data'))

    # Нормализуем имена колонок
    df.columns = [col.strip().lower().replace(' ', '_') for col in df.columns]

    # Заменяем все NaN на None (хотя мы снова приведём их позже)
    df = df.where(df.notna(), None)

    ti.xcom_push(key='clean_data', value=df.to_json())

def ensure_bigint_columns(cur, table, df):
    for col in df.columns:
        if pd.api.types.is_integer_dtype(df[col].dropna().dtype):
            try:
                cur.execute(
                    f"ALTER TABLE {table} ALTER COLUMN {col} TYPE BIGINT;"
                )
            except Exception:
                pass  # Уже BIGINT или недостаточно прав

def load_to_db(**kwargs):
    ti = kwargs['ti']
    # Читаем из XCom JSON-строку с "clean_data"
    df = pd.read_json(ti.xcom_pull(key='clean_data'))

    # Надёжно превращаем все numpy.nan в чистые Python None
    df = df.replace({np.nan: None})

    table = ti.xcom_pull(key='table')
    conn = get_connection()
    cur = conn.cursor()

    # Убедимся, что integer-колонки в БД — BIGINT
    ensure_bigint_columns(cur, table, df)
    conn.commit()

    # Подготавливаем bulk-insert
    cols = df.columns.tolist()
    values = [tuple(row) for row in df.to_numpy()]
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s"

    execute_values(cur, sql, values)

    conn.commit()
    cur.close()
    conn.close()

def load_to_file(**kwargs):
    # Не используется, оставлено для совместимости
    pass

default_args = {'start_date': datetime(2023, 1, 1)}

with DAG(
    dag_id="etl_csv_to_file",
    schedule_interval=None,
    default_args=default_args,
    catchup=False
) as dag:

    t1 = PythonOperator(
        task_id="extract",
        python_callable=extract,
        provide_context=True
    )
    t2 = PythonOperator(
        task_id="transform",
        python_callable=transform,
        provide_context=True
    )
    t3 = PythonOperator(
        task_id="load_to_db",
        python_callable=load_to_db,
        provide_context=True
    )

    t1 >> t2 >> t3
