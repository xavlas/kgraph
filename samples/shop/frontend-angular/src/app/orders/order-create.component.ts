import { Component } from '@angular/core';
import { Router } from '@angular/router';
import { OrderService } from './order.service';

@Component({
  selector: 'app-order-create',
  template: `<form (ngSubmit)="save()"><button>Create</button></form>`
})
export class OrderCreateComponent {
  model: any = {};
  constructor(private orderService: OrderService, private router: Router) {}

  save() {
    this.orderService.create(this.model).subscribe(() => this.router.navigate(['/orders']));
  }
}
