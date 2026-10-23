-- 统一重命名.app 的 AppleScript 源码
-- 编译：osacompile -o "$HOME/Applications/统一重命名.app" UnifiedRename.applescript
--
-- 需要 rnbatch 已在 PATH 中；dnd.sh 与本文件同目录时可直接跑，
-- 否则把 DND 路径改成你自己的位置。

on run
	display dialog "用法：把要改名的文件（或文件夹）拖到这个图标上。" buttons {"好"} default button 1 with title "统一重命名" with icon note
end run

on open theFiles
	set kw to text returned of (display dialog "请输入命名词（项目名 / 人名 / 公司简称）" default answer "" with title "统一重命名 · 第 1 步" buttons {"取消", "下一步"} default button 2)
	if kw is "" then return

	set tplList to {"{kw}-{n}", "{kw}-{n:02}-{type}", "{kw}-{orig}", "{kw}-{date}-{n}", "{type}-{kw}-{n}"}
	set picked to choose from list tplList with prompt "选一个命名模板" with title "统一重命名 · 第 2 步" default items {"{kw}-{n}"} without multiple selections allowed
	if picked is false then return
	set tpl to item 1 of picked

	set pathArgs to ""
	repeat with f in theFiles
		set pathArgs to pathArgs & " " & quoted form of (POSIX path of f)
	end repeat

	-- 把 dnd.sh 放在仓库的 integrations/macos/ 下，或用绝对路径替换这里
	set dnd to (POSIX path of (path to home folder)) & ".rnbatch-dnd.sh"

	set cmd to "bash " & quoted form of dnd & " " & quoted form of kw & " " & quoted form of tpl & pathArgs

	tell application "Terminal"
		activate
		do script cmd
	end tell
end open
