import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../environments/environment';

@Injectable({ providedIn: 'root' })
export class OrderService {
  private readonly baseUrl = `${environment.apiUrl}/orders`;

  constructor(private http: HttpClient) {}

  list(status?: string) {
    return this.http.get<any[]>(this.baseUrl, { params: { status: status ?? '' } });
  }

  get(id: number) {
    return this.http.get<any>(`${this.baseUrl}/${id}`);
  }

  create(order: any) {
    return this.http.post<any>(this.baseUrl, order);
  }

  cancel(id: number) {
    return this.http.post<void>(`${this.baseUrl}/${id}/cancel`, {});
  }
}
