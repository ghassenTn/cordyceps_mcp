// Next.js App Router — route.ts with exported GET/POST
import { NextRequest, NextResponse } from 'next/server';

export async function GET(request: NextRequest) {
  return NextResponse.json({ id: request.url });
}

export async function PUT(request: NextRequest) {
  return NextResponse.json({ updated: true });
}
