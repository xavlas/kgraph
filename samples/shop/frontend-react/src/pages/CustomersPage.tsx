import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { listCustomers } from '../api/customers';

export default function CustomersPage() {
  const [rows, setRows] = useState<any[]>([]);
  useEffect(() => {
    listCustomers().then(r => setRows(r.data));
  }, []);
  return (
    <div>
      <Link to="/customers/new">Register</Link>
      <ul>{rows.map(r => <li key={r.id}>{r.email}</li>)}</ul>
    </div>
  );
}
