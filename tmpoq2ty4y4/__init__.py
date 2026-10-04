"""Guest side of the sandbox: PID 1 init, JSON-RPC executor, handlers, AST checker.

Everything in this sub-package must stay **standard library only**: the sandbox
image installs nothing but ``python3-minimal``, and these modules are also the
blast radius if a tool manages to escalate inside the VM.
"""
