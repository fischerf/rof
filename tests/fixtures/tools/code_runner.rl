// code_runner.rl
// Demonstrates CodeRunnerTool: sandboxed Python execution.
// Trigger phrase: "run python code for <description>"

define Task as "A computation task to be executed as Python code".
define Result as "The output produced by running the Python script".

Task has language of "python".
Task has description of "Compute the first 10 Fibonacci numbers".
Task has timeout of 10.
Task has code of "from functools import reduce; print(reduce(lambda acc,_: acc+[acc[-1]+acc[-2]], range(8), [0,1]))".

Result has format of "stdout".

relate Task and Result as "produces".

ensure run python code for computing the first 10 fibonacci numbers.
ensure determine Result values.
