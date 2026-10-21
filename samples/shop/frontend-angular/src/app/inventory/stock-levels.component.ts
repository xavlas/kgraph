import { Component, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../environments/environment';

@Component({
  selector: 'app-stock-levels',
  template: `<h2>Stock levels</h2><ul><li *ngFor="let s of items">{{ s.sku }}</li></ul>`
})
export class StockLevelsComponent implements OnInit {
  items: any[] = [];
  constructor(private http: HttpClient) {}

  ngOnInit() {
    this.http.get<any[]>(`${environment.apiUrl}/inventory/levels`).subscribe(r => (this.items = r));
  }
}
