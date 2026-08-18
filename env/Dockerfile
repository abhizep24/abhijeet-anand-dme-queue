FROM python:3.12-slim

WORKDIR /srv
COPY env/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY env ./env
COPY data ./data

EXPOSE 8000
CMD ["uvicorn", "env.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
