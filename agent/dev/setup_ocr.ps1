# Project-local portable Tesseract; no installer execution or system PATH changes.
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$runtimeRoot = Join-Path $projectRoot 'tools/ocr'
$downloadRoot = Join-Path $runtimeRoot 'downloads'
$engineRoot = Join-Path $runtimeRoot 'tesseract'
$sevenZip = Get-Command '7z.exe' -ErrorAction SilentlyContinue
$sevenZipPath = if ($sevenZip) { $sevenZip.Source } else { 'C:/Program Files/7-Zip/7z.exe' }
if (-not (Test-Path -LiteralPath $sevenZipPath)) { throw '7-Zip이 필요합니다. 7z.exe 경로를 확인해 주세요.' }
New-Item -ItemType Directory -Force -Path $downloadRoot | Out-Null

function Get-VerifiedFile($url, $target, $sha256) {
    if (-not (Test-Path -LiteralPath $target)) { Invoke-WebRequest -Uri $url -OutFile $target }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $sha256) {
        throw "다운로드 파일의 SHA256이 다릅니다: $target"
    }
}
$installer = Join-Path $downloadRoot 'tesseract-5.5.0.exe'
Get-VerifiedFile 'https://github.com/tesseract-ocr/tesseract/releases/download/5.5.0/tesseract-ocr-w64-setup-5.5.0.20241111.exe' $installer 'F3FC4236425B690C8BE756F35793F77394EE004BE0A6460A440C754D892F68BC'
& $sevenZipPath x $installer "-o$engineRoot" -y | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Tesseract 압축 해제에 실패했습니다.' }
$dataRoot = Join-Path $engineRoot 'tessdata'
Get-VerifiedFile 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/kor.traineddata' (Join-Path $dataRoot 'kor.traineddata') '6B85E11D9BBF07863B97B3523B1B112844C43E713DF8B66418A081FD1060B3B2'
Get-VerifiedFile 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/eng.traineddata' (Join-Path $dataRoot 'eng.traineddata') '7D4322BD2A7749724879683FC3912CB542F19906C83BCC1A52132556427170B2'
Push-Location $dataRoot
try {
    & (Join-Path $engineRoot 'tesseract.exe') --tessdata-dir . --list-langs
    if ($LASTEXITCODE -ne 0) { throw 'OCR 언어 데이터 검사에 실패했습니다.' }
} finally { Pop-Location }
Write-Output '프로젝트 내부 Python OCR 준비 완료. Python 의존성은 uv sync로 설치하세요.'
