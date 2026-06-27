Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\Lenovo\Desktop\JARVIS_advanced\sidecars\whatsapp"
sh.Run """C:\Program Files\nodejs\node.EXE"" index.js", 0, False
