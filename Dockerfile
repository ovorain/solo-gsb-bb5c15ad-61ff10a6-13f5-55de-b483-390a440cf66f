FROM python:3.11-slim

# Image metadata; bump together with app.version in app/main.py.
LABEL org.opencontainers.image.title="adaptive-quiz-api" \
      org.opencontainers.image.version="1.11.0" \
      org.opencontainers.image.description="2PL IRT 计算机自适应测验 API（含题库批量导入/预检、题库修订历史、取题版本冻结按送达版本判分、单题质量复盘、题目撤回/启用、答卷雷同复核、单题预测概率校准、错题间隔复习 SM-2）"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QUIZ_DB_PATH=/data/quiz.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD python -c "import json,os,urllib.request; \
r=urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3); \
assert json.load(r)['status']=='ok'" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
