from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status, code, message=None, details=None):
        self.status = status
        self.code = code
        self.message = message or code
        self.details = details


def ok(data=None, status=200, message='요청이 처리되었습니다.'):
    return JSONResponse(status_code=status, content={'code': status, 'message': message, 'data': data})
