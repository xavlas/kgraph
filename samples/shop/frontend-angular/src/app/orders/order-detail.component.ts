import { Component, OnInit } from '@angular/core';
import { OrderService } from './order.service';

@Component({
  selector: 'app-order-detail',
  template: `<button (click)="cancel()">Cancel order</button>`
})
export class OrderDetailComponent implements OnInit {
  order: any;
  id = 0;
  constructor(private orderService: OrderService) {}

  ngOnInit() {
    this.orderService.get(this.id).subscribe(o => (this.order = o));
  }

  cancel() {
    this.orderService.cancel(this.id).subscribe();
  }
}
