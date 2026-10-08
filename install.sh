#!/bin/zsh
# Установщик Конвертера для macOS.
# Запуск одной командой в Терминале:
#   curl -fsSL https://raw.githubusercontent.com/mkkatrin/webp-converter/main/install.sh | zsh

REPO_RAW="https://raw.githubusercontent.com/mkkatrin/webp-converter/main"
APP="$HOME/webp-tool"

ask() { local a; read "a?$1" < /dev/tty; [[ "$a" == [дДyY]* ]]; }
finish() { echo; read "?Нажмите Enter, чтобы закрыть…" < /dev/tty; exit ${1:-0}; }

clear
echo "================================="
echo "   Установка Конвертера   "
echo "================================="
echo

# 1. Инструменты Apple (в них входит Python)
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Сначала нужно поставить инструменты разработчика Apple (в них есть Python)."
  echo "Сейчас откроется окно — нажмите «Установить» и дождитесь окончания."
  echo "Потом запустите установку ещё раз."
  xcode-select --install 2>/dev/null
  finish
fi

# 2. Приложение
echo "→ Скачиваю приложение"
mkdir -p "$APP"
curl -fsSL "$REPO_RAW/webp_app.py" -o "$APP/webp_app.py.tmp" && mv "$APP/webp_app.py.tmp" "$APP/webp_app.py" \
  || { echo "Не удалось скачать приложение. Проверьте интернет."; finish 1; }

# 3. Python-окружение и библиотеки
cd "$APP"
if [ ! -x .venv/bin/python ]; then
  echo "→ Создаю окружение Python"
  python3 -m venv .venv || { echo "Не удалось создать окружение Python"; finish 1; }
fi
echo "→ Устанавливаю библиотеки для картинок, PDF и PowerPoint (1–2 минуты)"
.venv/bin/pip install -q --disable-pip-version-check pillow pillow-heif pikepdf "pypdfium2>=5,<6" "python-pptx>=1,<2" \
  || { echo "Не удалось установить библиотеки. Проверьте интернет."; finish 1; }

# 4. Приложение «Конвертер» (запуск без Терминала)
.venv/bin/python webp_app.py --install >/dev/null || { echo "Не удалось создать приложение"; finish 1; }
echo "→ Приложение «Конвертер» создано: Рабочий стол и Launchpad"

# 5. ffmpeg для видео (необязательно)
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x $b ] && eval "$($b shellenv zsh)"; done
if command -v ffmpeg >/dev/null 2>&1; then
  echo "→ ffmpeg уже установлен — видео будут работать"
else
  echo
  echo "Для видео и GIF нужна программа ffmpeg. Картинки работают и без неё."
  if ask "Установить поддержку видео? (д/н): "; then
    if ! command -v brew >/dev/null 2>&1; then
      echo "→ Ставлю Homebrew. Он попросит пароль от Mac (символы при вводе не видны — это нормально)."
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" < /dev/tty
      for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        if [ -x $b ]; then
          eval "$($b shellenv zsh)"
          grep -q "brew shellenv" ~/.zprofile 2>/dev/null || echo "eval \"\$($b shellenv zsh)\"" >> ~/.zprofile
        fi
      done
    fi
    if command -v brew >/dev/null 2>&1; then
      echo "→ Устанавливаю ffmpeg (несколько минут)"
      brew install ffmpeg
    else
      echo "Homebrew не установился — видео можно будет добавить позже."
    fi
  fi
fi

echo
echo "================================="
echo "  Готово! Запускайте значок"
echo "  «Конвертер» на Рабочем столе."
echo "================================="
if ask "Открыть конвертер прямо сейчас? (д/н): "; then
  open "$HOME/Applications/Конвертер.app"; exit 0
fi
finish
