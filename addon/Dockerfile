FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Til at hente kode fra GitHub over HTTPS.
RUN apk add --no-cache ca-certificates

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY varmeopt ./varmeopt

# Startskallen hentes aldrig ned fra GitHub. Den skal kunne rydde op efter en
# selvopdatering hvis den hentede kode ikke engang kan importeres, og saa maa
# den ikke selv kunne vaere den oedelagte kode.
COPY bootstrap.py .

# /data/code staar foer /app, saa en hentet udgave vinder over den indbyggede.
# Arbejdsmappen flyttes vaek fra /app, fordi "python -m" ellers saetter /app
# forrest paa sys.path og saa ville den indbyggede kode altid vinde.
WORKDIR /
ENV PYTHONPATH=/data/code:/app

# Supervisor sender add-on'ens version fra config.yaml ind som build-argument.
# Ved at tage den den vej findes versionen kun ét sted, og loggen kan sige
# hvad der faktisk kører — ellers er en udrulning ikke til at efterprøve.
ARG BUILD_VERSION="ukendt"
ENV VARMEOPT_VERSION=${BUILD_VERSION} \
    VARMEOPT_BOOTSTRAP=/app/bootstrap.py

CMD ["python", "/app/bootstrap.py"]
