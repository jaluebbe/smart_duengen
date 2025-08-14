FROM python:3.10-slim-bullseye

WORKDIR /code

COPY ./requirements.txt /code/requirements.txt
RUN apt-get update && \
    apt-get install -y gdal-bin libgdal-dev g++ libgdal32 git && \
    pip install --upgrade pip && \
    pip install --no-cache-dir --upgrade -r /code/requirements.txt && \
    apt-get -y remove g++ gdal-bin libgdal-dev git && \
    apt -y autoremove && \
    apt-get update && apt-get upgrade -y && apt-get clean

COPY ./static /code/static
COPY ./backend_fastapi.py /code/

EXPOSE 80

CMD ["uvicorn", "backend_fastapi:app", "--host", "0.0.0.0", "--port", "80"]
