import { existsSync } from "node:fs";
import { resolve } from "node:path";

export function resolvePythonExecutable(projectRoot: string): string {
  if (process.env.QBR_PYTHON) return process.env.QBR_PYTHON;

  const virtualenvPython = process.platform === "win32"
    ? resolve(projectRoot, ".venv/Scripts/python.exe")
    : resolve(projectRoot, ".venv/bin/python");

  if (existsSync(virtualenvPython)) return virtualenvPython;
  return process.platform === "win32" ? "python" : "python3";
}
