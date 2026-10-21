import { Component, Input } from '@angular/core';

@Component({
  selector: 'app-order-row',
  template: `<a [routerLink]="['/orders', order.id]">#{{ order.id }} - {{ order.status }}</a>`
})
export class OrderRowComponent {
  @Input() order: any;
}
