FROM python:3.13-alpine

WORKDIR /app

# 标准库即可运行，无需任何 pip 依赖
COPY db.py cms.py crawler.py api.py ./

# 索引库挂载点（用 volume 持久化，容器重建不丢数据）
ENV INDEX_DB=/data/index.db \
    PORT=8090 \
    TZ=Asia/Shanghai

VOLUME /data
EXPOSE 8090

# 默认启动 API 服务；跑爬虫用: docker exec <c> python crawler.py --mode full
CMD ["python", "api.py"]
