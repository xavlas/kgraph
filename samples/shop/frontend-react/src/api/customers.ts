import axios from 'axios';

const API = '/api';

export const listCustomers = () => axios.get(`${API}/customers`);

export async function registerCustomer(payload: { email: string }) {
  return axios.post(`${API}/customers`, payload);
}
