FROM python:3.12-slim

WORKDIR /app

# Copier les dépendances
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copier le code
COPY app.py .

# Créer le dossier pour la base de données
RUN mkdir -p /data

# Variables d'environnement
ENV DATABASE_PATH=/data/overlord.db
ENV PORT=5000

# Exposer le port
EXPOSE 5000

# Lancer avec gunicorn pour la production
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "app:app"]
