import logging

import uvicorn

logging.basicConfig(
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)

from alexacart.app import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        "run:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=["alexacart", "."],
        reload_includes=["*.py", "*.html", ".env"],
    )
