"""Local folder routes are available only in the explicit local profile."""
from fastapi import APIRouter, Depends
from pydantic import Field
from .models import StrictModel
from . import local_workspace


class LocalImport(StrictModel):
    paths: list[str] = Field(min_length=1, max_length=20)
    request_id: str = Field(min_length=8, max_length=100)


def router(auth):
    result = APIRouter(dependencies=[Depends(auth)])

    @result.get('/api/local-workspace')
    def status():return local_workspace.status()

    @result.get('/api/local-workspace/files')
    def files(path: str = '', offset: int = 0, limit: int = 100):
        return local_workspace.files(path, offset, limit)

    @result.post('/api/projects/{project_id}/local-import')
    def import_files(project_id: str, value: LocalImport):
        return local_workspace.import_files(project_id, value.paths, value.request_id)

    @result.post('/api/projects/{project_id}/local-export')
    def export_project(project_id: str):return local_workspace.export_project(project_id)

    return result
