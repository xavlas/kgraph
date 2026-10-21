package com.acme.shop.customer;

import jakarta.persistence.*;
import jakarta.validation.constraints.Email;
import java.math.BigDecimal;

@Entity
@Table(name = "customers")
public class Customer {
    @Id @GeneratedValue
    private Long id;

    @Email
    private String email;

    private boolean active;

    @Column(name = "credit_limit")
    private BigDecimal creditLimit;

    public boolean isActive() { return active; }
    public BigDecimal getCreditLimit() { return creditLimit; }
}
