interface Repository<T> {
  find(id: string): T;
}

type ID = string;

export class UserRepo implements Repository<User> {
  find(id: ID): User {
    return new User();
  }
}

export class User {
  name: string = '';
}

export function findUser(id: ID): User {
  return new User();
}

export const cache = new Map<string, User>();
export const isDev = process.env.NODE_ENV === 'development';
