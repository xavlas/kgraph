package com.acme.shop.order;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import java.math.BigDecimal;

public class CreateOrderRequest {
    @NotNull
    private Long customerId;

    @NotBlank
    @Size(min = 3, max = 40, message = "SKU must be 3 to 40 characters")
    private String sku;

    @NotNull
    private Integer quantity;

    private BigDecimal amount;

    public Long getCustomerId() { return customerId; }
    public String getSku() { return sku; }
    public Integer getQuantity() { return quantity; }
    public BigDecimal getAmount() { return amount; }
}
