import uvicorn
from .service import create_app

uvicorn.run(create_app(), host='0.0.0.0', port=8090, access_log=False)
