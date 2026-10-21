import { Component, OnInit } from '@angular/core';
import { OrderService } from './order.service';

@Component({
  selector: 'app-order-list',
  templateUrl: './order-list.component.html'
})
export class OrderListComponent implements OnInit {
  orders: any[] = [];
  constructor(private orderService: OrderService) {}

  ngOnInit() {
    this.orderService.list().subscribe(o => (this.orders = o));
  }
}
