@echo off
REM ============================================================
REM  闲鱼超级管家 - 回归测试入口（Windows 源码运行）
REM
REM  说明：本机（WorkBuddy 沙箱）的 shell 没有设置 PATHEXT 环境变量，
REM  而 PyExecJS 依赖 PATHEXT 去拼 node.exe 的扩展名，缺了就会报
REM  「Could not find an available JavaScript runtime」，导致 19 个用例
REM  直接 error、另有 52 个用例连加载都失败。这里显式补上。
REM
REM  正常 Windows 终端（cmd / PowerShell / 资源管理器双击）本来就有
REM  PATHEXT，可以不加这一行。
REM ============================================================

set "PATHEXT=.COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"

cd /d "%~dp0.."

echo [1/2] 字节码编译检查...
".venv\Scripts\python.exe" -m compileall -q Start.py XianyuAutoAsync.py app utils tests
if errorlevel 1 (
    echo    编译检查失败
) else (
    echo    编译检查通过
)

echo.
echo [2/2] 运行单元测试...
".venv\Scripts\python.exe" -m unittest discover -s tests -p "test_*.py" -v
