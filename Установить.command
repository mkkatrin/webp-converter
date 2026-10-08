#!/bin/zsh
# Установщик WebP Конвертера: скачивает и запускает актуальную версию установки.
curl -fsSL https://raw.githubusercontent.com/mkkatrin/webp-converter/main/install.sh -o /tmp/webp-install.sh \
  && exec zsh /tmp/webp-install.sh
echo "Не удалось скачать установщик. Проверьте интернет."
read "?Нажмите Enter, чтобы закрыть…"
