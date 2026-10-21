import { BrowserRouter, Routes, Route } from 'react-router-dom';
import CustomersPage from './pages/CustomersPage';
import CustomerFormPage from './pages/CustomerFormPage';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/customers" element={<CustomersPage />} />
        <Route path="/customers/new" element={<CustomerFormPage />} />
      </Routes>
    </BrowserRouter>
  );
}
