"""배포 진입점 shim — 차트의 `python controller.py`를 무수정으로 유지한다. 실체는 entrypoints/controller.py."""
from entrypoints.controller import main

if __name__ == "__main__":
    main()
