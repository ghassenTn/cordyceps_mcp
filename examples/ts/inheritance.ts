interface IFace {
  run(): void;
}

class Base {
  base() {
    return 'base';
  }
}

export class Child extends Base {
  child() {
    return 'child';
  }
}

export class Impl implements IFace {
  run() {
    return 'run';
  }
}

export class Impl2 implements IFace, Other {
  run() {
    return 'run';
  }
}
