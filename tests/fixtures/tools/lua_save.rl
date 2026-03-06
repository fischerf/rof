// lua_save.rl
// Demonstrates FileSaveTool: writes arbitrary text content to a file on disk.
// Trigger phrase: "save file"

define Script as "A Lua source file to be saved to disk".

Script has file_path of "/tmp/hello.lua".
Script has content of "print('Hello from ROF FileSaveTool!')".

ensure save file.
