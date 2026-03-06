// lua_run.rl
// Demonstrates LuaRunTool: runs a saved Lua script interactively in the terminal.
// Trigger phrase: "run lua script"
//
// This stage expects lua_save.rl to have already written Script.file_path
// into the snapshot via FileSaveTool.

define Script as "A Lua source file previously saved to disk".

Script has file_path of "/tmp/hello.lua".

ensure run lua script.
