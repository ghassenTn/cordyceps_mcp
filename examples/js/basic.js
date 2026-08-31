// CommonJS + ES modules mixed
const util = require('util');

export function add(a, b) {
  return a + b;
}

export function multiply(a, b) {
  return a * b;
}

export const subtract = (a, b) => a - b;

export const PI = 3.14159;

export class Calculator {
  constructor() {
    this.total = 0;
  }
  add(value) {
    this.total += value;
    return this;
  }
  reset() {
    this.total = 0;
  }
}

const helper = () => 'hidden';
export { helper };

export async function fetchData(url) {
  return url;
}
