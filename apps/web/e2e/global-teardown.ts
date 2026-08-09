import { rmSync } from "node:fs";
import { basename, dirname } from "node:path";
import { tmpdir } from "node:os";

export default function globalTeardown() {
  const dataDir = process.env.QBR_E2E_DATA_DIR;
  if (!dataDir) return;
  if (dirname(dataDir) !== tmpdir() || !basename(dataDir).startsWith("qbr-playwright-")) {
    throw new Error(`Refusing to remove unexpected E2E data directory: ${dataDir}`);
  }
  rmSync(dataDir, { recursive: true, force: true });
}
