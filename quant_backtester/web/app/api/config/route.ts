import { NextResponse } from "next/server";
import { applyEdits, readAllConfigValues, type Edit } from "@/lib/config-io";
import { CONFIG_FILES } from "@/lib/config-schema";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return NextResponse.json({ schema: CONFIG_FILES, values: await readAllConfigValues() });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "failed to read config" },
      { status: 500 }
    );
  }
}

export async function PATCH(request: Request) {
  let body: { edits?: Edit[] };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }

  if (!Array.isArray(body.edits)) {
    return NextResponse.json({ error: "expected an `edits` array" }, { status: 400 });
  }

  try {
    const { written } = await applyEdits(body.edits);
    return NextResponse.json({ ok: true, written, values: await readAllConfigValues() });
  } catch (error) {
    // Validation failures are the user's problem to fix, not a server fault.
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "failed to save config" },
      { status: 400 }
    );
  }
}
