FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY varmeopt ./varmeopt

# Supervisor sender add-on'ens version fra config.yaml ind som build-argument.
# Ved at tage den den vej findes versionen kun ét sted, og loggen kan sige
# hvad der faktisk kører — ellers er en udrulning ikke til at efterprøve.
ARG BUILD_VERSION="ukendt"
ENV VARMEOPT_VERSION=${BUILD_VERSION}

CMD ["python", "-m", "varmeopt"]
