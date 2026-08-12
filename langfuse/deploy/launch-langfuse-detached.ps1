# 经 WMI 在独立会话拉起 Langfuse：父进程为 WmiPrvSE（系统服务），
# 不挂在任何终端/后端的进程树下，外部控制台中断信号无法波及。
# run-langfuse.ps1 自带幂等判断（已就绪则直接退出），本脚本可放心重复执行。
$script = Join-Path $PSScriptRoot 'run-langfuse.ps1'
$cmd = "`"C:\Program Files\PowerShell\7\pwsh.exe`" -NoProfile -ExecutionPolicy Bypass -File `"$script`""
# ShowWindow=0(SW_HIDE)：宿主控制台窗口完全不可见。
# 此前窗口可见，被手动关闭时 CTRL_CLOSE 事件（0xC000013A）会团灭整棵进程树
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [UInt16]0 }
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $cmd; ProcessStartupInformation = $startup 
}
if ($r.ReturnValue -ne 0) {
    Write-Error "WMI 拉起 Langfuse 失败，ReturnValue=$($r.ReturnValue)"
    exit 1
}
Write-Output "Langfuse 已在独立会话拉起（pid=$($r.ProcessId)）"
