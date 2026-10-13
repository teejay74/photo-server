# Личная фотогалерея

Минималистичный веб-интерфейс для локального каталога фотографий. Работает на Python 3, оригиналы не изменяет.

## Запуск

```bash
export PHOTO_ROOT=/srv/photos/xiaomi-13t/Camera
export PHOTO_USER=photos
export PHOTO_PASSWORD='change-me'
python3 server.py --host 0.0.0.0 --port 8080
```

Pillow опционален. С ним приложение создаёт и кэширует уменьшенные копии; без него в сетке отображаются оригиналы.

## Переменные окружения

- `PHOTO_ROOT` — каталог оригиналов.
- `PHOTO_CACHE` — каталог кэша миниатюр.
- `PHOTO_USER` и `PHOTO_PASSWORD` — HTTP Basic Auth. Пустой пароль отключает авторизацию.
- `PHOTO_HOST`, `PHOTO_PORT` — адрес и порт.
