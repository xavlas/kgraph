import { Routes } from '@angular/router';
import { OrderListComponent } from './orders/order-list.component';
import { OrderCreateComponent } from './orders/order-create.component';
import { OrderDetailComponent } from './orders/order-detail.component';
import { AuthGuard } from './auth.guard';

export const routes: Routes = [
  { path: '', redirectTo: 'orders', pathMatch: 'full' },
  {
    path: 'orders',
    children: [
      { path: '', component: OrderListComponent },
      { path: 'new', component: OrderCreateComponent, canActivate: [AuthGuard] },
      { path: ':id', component: OrderDetailComponent }
    ]
  },
  { path: 'inventory', loadChildren: () => import('./inventory/inventory.routes').then(m => m.INVENTORY_ROUTES) }
];
