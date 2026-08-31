// tRPC router with .query() and .mutation()
import { initTRPC } from '@trpc/server';

const t = initTRPC.create();

export const appRouter = t.router({
  users: t.procedure.query(() => []),
  createUser: t.procedure.mutation(() => ({ id: 1 })),
  userById: t.procedure.input(String).query(({ input }) => ({ id: input })),
});

export type AppRouter = typeof appRouter;
