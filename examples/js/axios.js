import axios from 'axios';

export async function getUser(id) {
  return axios.get(`/api/users/${id}`);
}

export async function createUser(data) {
  return axios.post('/api/users', data);
}

export function loadDashboard() {
  return fetch('/dashboard').then((r) => r.json());
}
