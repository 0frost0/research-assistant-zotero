"""网页启动入口，同时兼容 uvicorn web_app:app。"""

import uvicorn

from research_assistant.web.web_app import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765)
