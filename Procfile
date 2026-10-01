web: cd config && gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --threads 4 --worker-class gthread --preload --timeout 300
