/**
 * Pipeline status — which layers have been run and what they produced.
 *
 * Renamed from /api/status to avoid a naming collision with the Flask app's
 * /api/status endpoint (which returns wave-extractor process state). Both
 * services can sit behind the same reverse proxy without their routes
 * clobbering each other.
 *
 * Accepts an optional `?universe=<id>` query param so the console always
 * reports status for the universe currently on screen, not the default one.
 */

import { NextResponse, type NextRequest } from "next/server";
import { getPipelineStatus } from "@/lib/artifacts";
import { resolveUniverse } from "@/lib/universes";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const universe = resolveUniverse(
    request.nextUrl.searchParams.get("universe") ?? undefined
  );
  return NextResponse.json(await getPipelineStatus(universe));
}
