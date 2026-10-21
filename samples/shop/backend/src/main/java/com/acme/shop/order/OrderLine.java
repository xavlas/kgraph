package com.acme.shop.order;

import jakarta.persistence.*;
import jakarta.validation.constraints.Min;

@Entity
@Table(name = "order_lines")
public class OrderLine {
    @Id @GeneratedValue
    private Long id;

    @ManyToOne
    @JoinColumn(name = "order_id")
    private Order order;

    @Column(name = "sku")
    private String sku;

    @Min(1)
    private int quantity;
}
