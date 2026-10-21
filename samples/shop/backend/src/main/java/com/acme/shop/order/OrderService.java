package com.acme.shop.order;

import com.acme.shop.common.BusinessException;
import com.acme.shop.common.NotFoundException;
import com.acme.shop.customer.CustomerService;
import java.math.BigDecimal;
import java.util.List;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class OrderService {
    private static final BigDecimal MAX_ORDER_AMOUNT = new BigDecimal("10000");

    private final OrderRepository orderRepository;
    private final CustomerService customerService;
    private final KafkaTemplate<String, String> kafkaTemplate;

    public OrderService(OrderRepository orderRepository, CustomerService customerService,
                        KafkaTemplate<String, String> kafkaTemplate) {
        this.orderRepository = orderRepository;
        this.customerService = customerService;
        this.kafkaTemplate = kafkaTemplate;
    }

    public List<Order> list(OrderStatus status) {
        return status == null ? orderRepository.findAll() : orderRepository.findByStatus(status);
    }

    public Order get(Long id) {
        return orderRepository.findById(id).orElseThrow(() -> new NotFoundException("Order " + id + " does not exist"));
    }

    @Transactional
    public Order create(CreateOrderRequest request) {
        // BR-ORD-01: an order may only be placed for an active customer
        customerService.assertActive(request.getCustomerId());
        BigDecimal amount = request.getAmount();
        if (amount.compareTo(MAX_ORDER_AMOUNT) > 0) {
            throw new BusinessException("Order amount exceeds the maximum of 10000");
        }
        BigDecimal outstanding = orderRepository.outstandingAmount(request.getCustomerId());
        if (outstanding.add(amount).compareTo(customerService.creditLimit(request.getCustomerId())) > 0) {
            throw new BusinessException("Customer credit limit exceeded");
        }
        Order order = new Order();
        order.setStatus(OrderStatus.NEW);
        order.setTotalAmount(amount);
        Order saved = orderRepository.save(order);
        kafkaTemplate.send("${app.topics.order-created}", String.valueOf(saved.getStatus()));
        return saved;
    }

    @Transactional
    public void cancel(Long id) {
        Order order = get(id);
        if (order.getStatus() == OrderStatus.SHIPPED) {
            throw new BusinessException("A shipped order cannot be cancelled");
        }
        orderRepository.markCancelled(id);
    }
}
