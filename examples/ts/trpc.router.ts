import { initTRPC } from '@trpc/server';

const t = initTRPC.create();

export const appRouter = t.router({
  listPosts: t.procedure.query(() => []),
  getPost: t.procedure.input(String).query(({ input }) => input),
  createPost: t.procedure.mutation(() => ({ ok: true })),
});

export type AppRouter = typeof appRouter;
