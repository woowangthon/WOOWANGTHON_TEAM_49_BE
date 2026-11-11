# 한글 보고서 폰트

`NotoSansKR-Regular.ttf`는 Google Fonts 공식 Noto Sans KR 가변 TTF에서 FontTools로 wght=400 인스턴스를 생성한 파일이다. Windows 시스템 폰트나 원본 프로젝트 자산을 복사하지 않았다. 배포 라이선스는 동봉 `OFL.txt`다.

- 원본: https://github.com/google/fonts/tree/main/ofl/notosanskr
- 원본 파일: `NotoSansKR[wght].ttf`
- 수집일: 2026-11-09
- 원본 SHA-256: `194018e6b2b293a7964f037b25c0249ce1418bc9ab3c971060a03aa57861e252`
- 생성: `fontTools.varLib.instancer.instantiateVariableFont(font, {'wght': 400}, inplace=True, updateFontNames=True)`

앱 시작 시 `AGENT_FONT_PATH`를 agent settings에 주입한다. 기본값은 통합본 기준 상대경로 `assets/fonts/NotoSansKR-Regular.ttf`다.
