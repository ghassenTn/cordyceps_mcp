// Next.js Pages Router — filename-based export default handler + named HTTP exports
import { NextApiRequest, NextApiResponse } from 'next';

export default function handler(req: NextApiRequest, res: NextApiResponse) {
  res.status(200).json({ users: [] });
}

export async function GET(request: NextApiRequest) {
  return { users: [] };
}

export async function POST(request: NextApiRequest) {
  return { created: true };
}
