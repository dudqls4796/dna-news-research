' 로그온 시 임베딩 트리거 서버(embed_server.py, 127.0.0.1:8011)를 창 없이 실행한다.
' 시작프로그램 폴더(shell:startup)에 이 파일의 바로가기를 두어 사용. 로그: %TEMP%\dice_embed_server.log
' 이미 8011 포트가 사용 중이면 서버가 바로 종료되므로 중복 실행돼도 문제없다.
Set fso = CreateObject("Scripting.FileSystemObject")
dbDir = fso.GetParentFolderName(WScript.ScriptFullName)
py = fso.BuildPath(fso.GetParentFolderName(dbDir), ".venv\Scripts\python.exe")
logFile = CreateObject("WScript.Shell").ExpandEnvironmentStrings("%TEMP%") & "\dice_embed_server.log"
cmd = "cmd /c cd /d """ & dbDir & """ && set ""PYTHONUTF8=1"" &&""" & py & """ embed_server.py >> """ & logFile & """ 2>&1"
CreateObject("WScript.Shell").Run cmd, 0, False
