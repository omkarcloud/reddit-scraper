FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .

# Comment pages are read through a real Chrome (reddit/browser.py): patchright's
# Chromium with its system libraries, plus Xvfb as its display — Reddit walls
# headless browsers.
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries \
 && python -m pip install --no-cache-dir -r requirements.txt \
 && python -m patchright install --with-deps chromium \
 && apt-get update && apt-get install -y --no-install-recommends xvfb xauth \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir app
WORKDIR /app
COPY . /app

EXPOSE 8000

# Xvfb started directly (xvfb-run as PID 1 waits forever for Xvfb's ready signal);
# Chrome starts on the first comment request, long after the display is up.
ENV DISPLAY=:99
CMD ["sh", "-c", "Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp & exec python run.py"]
