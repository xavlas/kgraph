import { registerCustomer } from '../api/customers';

export default function CustomerFormPage() {
  const submit = async (email: string) => {
    await registerCustomer({ email });
  };
  return <form onSubmit={() => submit('a@b.c')}><button>Save</button></form>;
}
