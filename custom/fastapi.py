from fastapi import FastAPI
from custom.routers import router as userspace_router
app =  FastAPI()

app.include_router(userspace_router)