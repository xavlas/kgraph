package com.acme.shop.order;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;

public interface OrderRepository extends JpaRepository<Order, Long> {
    List<Order> findByStatus(OrderStatus status);

    List<Order> findByCustomerIdAndStatus(Long customerId, OrderStatus status);

    @Query(value = "SELECT COALESCE(SUM(o.total_amount), 0) FROM orders o WHERE o.customer_id = :customerId AND o.status <> 'CANCELLED'", nativeQuery = true)
    java.math.BigDecimal outstandingAmount(Long customerId);

    @Modifying
    @Query("update Order o set o.status = 'CANCELLED' where o.id = :id")
    int markCancelled(Long id);
}
